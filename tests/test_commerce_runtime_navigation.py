"""The parts of paging that need no database, and the lines it must not cross.

The durable half — single use, expiry, scope, races, cleanup, the migration —
is proven against real PostgreSQL in
``tests/commerce_reliability/test_commerce_runtime_navigation_pg.py``, and the
whole flow through the real runtime in
``tests/commerce_reliability/test_commerce_runtime_pagination_pg.py``. What is
here is everything a store cannot help with:

* the page arithmetic partitions any result, with no page over ten rows;
* the typed search contract refuses every way it could be misused, and never
  reaches the model — not in a result, not in a checkpoint, not in a digest;
* a database without revision 0113 offers the model byte-for-byte the
  declarations it offered before;
* a browse opens only when the model gave both its words over products of one
  search that has more, and never expands a focused answer;
* the two row namespaces stay disjoint, and a "More" row is never an option;
* cleanup has a registered owner.

Merchant-agnostic: products are opaque integers, and rotating generic categories
where a title is needed at all.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import agent_live_tools as alt  # noqa: E402
from core.commerce_runtime import agent_loop as al  # noqa: E402
from core.commerce_runtime import agent_provider as ap  # noqa: E402
from core.commerce_runtime import agent_tools as at  # noqa: E402
from core.commerce_runtime import browse as br  # noqa: E402
from core.commerce_runtime import choice_rows as cr  # noqa: E402
from core.commerce_runtime import contracts as c  # noqa: E402
from core.commerce_runtime import navigation as nav  # noqa: E402
from core.commerce_runtime import navigation_models as nm  # noqa: E402
from core.commerce_runtime import presentation_policy as pp  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402
from core.commerce_runtime import search_candidates as sc  # noqa: E402

TITLES = ("قميص قطني أزرق", "حذاء رياضي أبيض", "عطر ورد 100ml", "حزام جلد بني",
          "ساعة يد كلاسيكية", "نظارة شمسية", "حقيبة كتف", "وشاح صوف", "قبعة صيفية",
          "معطف خفيف", "سترة رياضية", "جوارب قطنية")
TENANT, CONVERSATION, TURN = 7, 70, 700
SCOPE = at.ToolScope(tenant_id=TENANT, namespace="live", conversation_id=CONVERSATION, turn_id=TURN)


def product(product_id: int, *, orderable: bool = True) -> Dict[str, Any]:
    return {"product_id": product_id, "title": f"{TITLES[product_id % len(TITLES)]} {product_id}",
            "price": str(100 + product_id), "currency": "SAR", "orderable": orderable,
            "evidence_ref": rc.product_ref(product_id)}


def candidates(ids: Sequence[int], window: Sequence[int], *, complete: bool = True,
               **scope: Any) -> sc.SearchCandidates:
    bound = {"tenant_id": TENANT, "namespace": "live", "conversation_id": CONVERSATION,
             "turn_id": TURN, **scope}
    return sc.SearchCandidates(query_digest=sc.query_digest("q"), method="fts",
                               product_ids=tuple(ids), complete=complete,
                               window_ids=tuple(window), **bound)


def search(window: Sequence[int], platform: Any, *, call_id: str = "s1",
           tool: str = "search_products", unorderable: Sequence[int] = ()) -> ac.ToolObservation:
    rows = [product(i, orderable=i not in unorderable) for i in window]
    return ac.ToolObservation(call_id=call_id, tool_name=tool, ok=True,
                              result={"status": "ok", "found": True, "products": rows},
                              error_code=None, error=None,
                              evidence_refs=tuple(r["evidence_ref"] for r in rows),
                              platform=platform)


def draft(ids: Sequence[int], *, more: Optional[str] = "More", button: Optional[str] = "Pick",
          text: str = "Some options.") -> ac.ReplyDraft:
    request: Dict[str, Any] = {"product_ids": list(ids)}
    if more is not None:
        request["more_label"] = more
    if button is not None:
        request["button"] = button
    return ac.ReplyDraft(text=text, evidence_refs=tuple(rc.product_ref(i) for i in ids),
                         claims_commerce_facts=True, payload={rc.REQUESTED_KEY: request})


@dataclasses.dataclass
class Reader:
    """A stand-in for the trusted catalogue read, recording what it was asked."""

    gone: Sequence[int] = ()
    unorderable: Sequence[int] = ()
    fail: bool = False
    asked: List[List[int]] = dataclasses.field(default_factory=list)

    def __call__(self, scope: Any, ids: Sequence[int], wait: float) -> alt.ListRowsRead:
        self.asked.append(list(ids))
        if self.fail:
            return alt.ListRowsRead(ok=False, error="timeout")
        return alt.ListRowsRead(
            ok=True, products=tuple(product(i, orderable=i not in self.unorderable)
                                    for i in ids if i not in self.gone),
            missing=tuple(i for i in ids if i in self.gone))


def runtime(reader: Reader) -> br.BrowseRuntime:
    return br.BrowseRuntime(read_rows=reader, search_tool_names=("search_products",))


def listed(selection: rc.ChoiceSelection) -> List[int]:
    return [rc.product_id_from_row_id(row["id"]) for row in selection.rows
            if rc.product_id_from_row_id(row["id"]) is not None]


def more_rows(selection: rc.ChoiceSelection) -> List[Mapping[str, Any]]:
    return [row for row in selection.rows if nav.is_navigation_row(dict(row))]


# ── Page arithmetic ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("total", list(range(0, 61)))
def test_the_pages_of_any_result_partition_it_with_no_page_over_ten(total):
    offset, pages, seen = 0, [], []
    while True:
        bounds = nav.page_bounds(total, offset)
        if bounds.start >= bounds.end:
            break
        size = bounds.end - bounds.start
        rows = size + (1 if bounds.has_next else 0)
        assert rows <= nm.MAX_ROWS
        assert size == nm.PAGE_SIZE if bounds.has_next else size <= nm.MAX_ROWS
        assert bounds.number == len(pages) + 1
        pages.append(size)
        seen.extend(range(bounds.start, bounds.end))
        if not bounds.has_next:
            break
        offset = bounds.end
    assert seen == list(range(total)), "every position exactly once, in order"
    if 0 < total <= 10:
        assert pages == [total]


def test_the_boundaries_named_in_the_assignment():
    assert [nav.page_bounds(10, 0).has_next, nav.page_bounds(11, 0).has_next] == [False, True]
    assert nav.page_bounds(11, 9) == nav.PageBounds(start=9, end=11, has_next=False)
    assert nav.page_bounds(19, 9) == nav.PageBounds(start=9, end=19, has_next=False)
    assert nav.page_bounds(20, 9) == nav.PageBounds(start=9, end=18, has_next=True)


# ── Row namespaces ───────────────────────────────────────────────────────────


def test_the_two_row_namespaces_are_disjoint():
    token = nav.new_token()
    assert rc.product_id_from_row_id(nav.row_id(token)) is None
    assert nav.token_from_row_id(rc.row_id(42)) is None
    assert nav.token_from_row_id(nm.NAVIGATION_ROW_PREFIX) is None
    assert nav.token_from_row_id(nm.NAVIGATION_ROW_PREFIX + "x" * 65) is None
    assert nav.token_from_row_id(nav.row_id(token)) == token


def test_a_more_row_is_never_carried_as_an_option_line():
    payload = {"text": "Options:", rc.CHOICES_KEY: {
        "rows": [{"id": rc.row_id(1), "title": "قميص قطني أزرق"},
                 {"id": nav.row_id(nav.new_token()), "title": "More"}],
        "product_ids": [1], "button": "Pick"}}
    recovered = rc.text_only_payload(payload)
    assert "قميص قطني أزرق" in recovered["text"] and "More" not in recovered["text"]


def test_a_numbered_label_on_a_later_page_states_its_place_in_the_browse():
    alike = [{"product_id": i, "title": "فستان"} for i in range(1, 4)]
    first = cr.choice_rows(alike)
    later = cr.choice_rows(alike, start_position=10)
    assert [row.title for row in first.rows] == ["فستان · 1", "فستان · 2", "فستان · 3"]
    assert [row.title for row in later.rows] == ["فستان · 10", "فستان · 11", "فستان · 12"]


# ── The typed search contract ────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    {"product_ids": (1, 1)}, {"product_ids": (True, 2)}, {"product_ids": (0,)},
    {"product_ids": tuple(range(1, sc.CANDIDATE_CAP + 2))}, {"tenant_id": 0},
    {"namespace": ""}, {"complete": "yes"}, {"window_ids": (3, 3)},
])
def test_the_contract_refuses_a_value_that_is_not_one(bad):
    fields = {"tenant_id": TENANT, "namespace": "live", "conversation_id": CONVERSATION,
              "turn_id": TURN, "query_digest": "d", "method": "fts",
              "product_ids": (1, 2, 3), "complete": True, "window_ids": (1,)}
    fields.update(bad)
    with pytest.raises((ValueError, TypeError)):
        sc.SearchCandidates(**fields)


def _usable(obs: ac.ToolObservation) -> str:
    return sc.from_observation(obs, tenant_id=TENANT, namespace="live",
                               conversation_id=CONVERSATION, turn_id=TURN,
                               search_tool_names=("search_products",))[1]


def test_candidates_are_used_only_from_a_search_in_this_scope_that_agrees_with_its_window():
    ids = list(range(1, 24))
    good = search(ids[:5], candidates(ids, ids[:5]))
    assert _usable(good) == sc.USABLE
    assert _usable(search(ids[:5], candidates(ids, ids[:5]), tool="get_order_details")) == sc.NOT_A_SEARCH
    assert _usable(dataclasses.replace(good, ok=False)) == sc.NOT_A_SEARCH
    assert _usable(dataclasses.replace(good, body_truncated=True)) == sc.NOT_A_SEARCH
    assert _usable(search(ids[:5], None)) == sc.NO_CANDIDATES
    assert _usable(search(ids[:5], list(ids))) == sc.NO_CANDIDATES, "an untyped list is not a contract"
    for other in ({"tenant_id": TENANT + 1}, {"conversation_id": CONVERSATION + 1},
                  {"turn_id": TURN + 1}, {"namespace": "shadow"}):
        assert _usable(search(ids[:5], candidates(ids, ids[:5], **other))) == sc.WRONG_SCOPE
    # The model was shown products the stored result does not start with.
    assert _usable(search(ids[5:10], candidates(ids, ids[5:10]))) == sc.WINDOW_NOT_PREFIX
    assert _usable(search([99] + ids[:4], candidates(ids, ids[:5]))) == sc.WINDOW_NOT_PREFIX


def test_the_contract_never_reaches_the_model():
    ids = list(range(1, 24))
    obs = search(ids[:5], candidates(ids, ids[:5]))
    session = al._Session(scope=al._Scope(TENANT, "live", CONVERSATION, TURN),
                          token=c.OwnershipToken(owner_id="t", fence=1, epoch=1, tenant_id=TENANT,
                                                 namespace="live", conversation_id=CONVERSATION),
                          requested=ac.LoopBudget(), clock=time.monotonic)
    session.observations = [obs]
    shown = session.provider_observations()[0]
    assert shown.platform is None
    serialized = json.dumps([ap._observation_payload(shown),
                             [cp.to_payload() for cp in ac.checkpoint_observations([obs])]])
    for hidden in ids[5:]:
        assert f'"product_id": {hidden}' not in serialized
    # Nothing of the contract's own shape either: its field names, or the
    # stored result as a list, would be how a leak serialises.
    for field in ("product_ids", "window_ids", "query_digest", "complete", "SearchCandidates"):
        assert field not in serialized
    assert json.dumps(ids) not in serialized and json.dumps(ids)[1:-1] not in serialized
    assert ac.observation_digest(obs) == ac.observation_digest(dataclasses.replace(obs, platform=None))
    assert all(restored.platform is None
               for restored in (cp.restore() for cp in ac.checkpoint_observations([obs])))


# ── Dormant without revision 0113 ────────────────────────────────────────────


def test_without_paging_the_reply_declaration_is_byte_for_byte_what_it_was():
    assert ap.reply_tool_schema(paging=False) is ap.REPLY_TOOL_SCHEMA
    assert "more_label" not in json.dumps(ap.REPLY_TOOL_SCHEMA)
    with_paging = ap.reply_tool_schema(paging=True)
    assert with_paging["properties"]["choices"]["properties"]["more_label"] == ap.MORE_LABEL_PROPERTY
    assert "more_label" not in json.dumps(ap.REPLY_TOOL_SCHEMA), "the shared schema was mutated"


def test_without_paging_the_search_declaration_is_what_it_was():
    class _Link:
        tenant_id, namespace, runtime_conversation_id = TENANT, "live", CONVERSATION

    off = {t.definition.name: t.definition.description
           for t in alt.build_live_tools(alt.LiveToolBinding(context=None, link=_Link()))}
    on = {t.definition.name: t.definition.description
          for t in alt.build_live_tools(alt.LiveToolBinding(context=None, link=_Link(),
                                                            paging_available=True))}
    declared = {name: description for name, description, *_ in alt._DECLARATIONS}
    assert off == declared
    assert on[alt.SEARCH_TOOL_NAME] == declared[alt.SEARCH_TOOL_NAME] + alt.SEARCH_PAGING_NOTE
    assert {k: v for k, v in on.items() if k != alt.SEARCH_TOOL_NAME} == \
        {k: v for k, v in declared.items() if k != alt.SEARCH_TOOL_NAME}


def test_the_models_more_word_is_carried_as_a_request_and_bounded():
    request = ap._requested_choices({"product_ids": [1, 2], "button": "Pick",
                                     "more_label": "  " + "See more " * 6})
    assert request[rc.REQUESTED_KEY]["more_label"] == ("See more " * 6).strip()[:rc.MAX_ROW_TITLE]
    assert "more_label" not in ap._requested_choices({"product_ids": [1]})[rc.REQUESTED_KEY]


# ── Opening a browse ─────────────────────────────────────────────────────────


def test_page_one_is_the_stored_head_and_the_platform_hydrates_only_what_the_model_did_not_see():
    ids = list(range(101, 124))
    reader = Reader()
    composed, reason = br.open_browse(draft(ids[:5]), [search(ids[:5], candidates(ids, ids[:5]))],
                                      scope=SCOPE, runtime=runtime(reader), timeout_seconds=5)
    assert reason == br.OPENED and composed is not None
    assert listed(composed.selection) == ids[:9]
    assert reader.asked == [ids[5:9]]
    (more,) = more_rows(composed.selection)
    assert more["title"] == "More" and composed.selection.button == "Pick"
    mint = composed.plan.mint
    assert mint.product_ids == tuple(ids) and mint.page_offset == 9 and not composed.plan.spend
    assert nav.token_from_row_id(more["id"]) == mint.token
    assert mint.origin_call_id == "s1" and mint.origin_turn_id == TURN
    assert composed.navigation == {"page": 1, "offset": 0, "shown": 9, "unavailable": 0,
                                   "has_next": True, "complete": True, "stored": 23}


def test_a_result_that_fits_one_list_is_offered_whole_and_stores_nothing():
    ids = list(range(1, 11))
    composed, reason = br.open_browse(draft(ids[:5]), [search(ids[:5], candidates(ids, ids[:5]))],
                                      scope=SCOPE, runtime=runtime(Reader()), timeout_seconds=5)
    assert reason == br.WHOLE and listed(composed.selection) == ids
    assert not more_rows(composed.selection) and not composed.plan


@pytest.mark.parametrize("case,expected", [
    ("no_selector", br.NO_SELECTOR),
    ("no_more_word", br.WORDS_MISSING),
    ("no_button_word", br.WORDS_MISSING),
    ("models_pick", br.MODEL_PICK),
    ("two_searches", br.NO_CONTINUATION),
    ("other_scope", br.NO_CONTINUATION),
    ("nothing_more", br.NOTHING_MORE),
    ("catalogue_unreadable", br.ROWS_UNAVAILABLE),
])
def test_a_browse_is_not_opened_unless_every_part_of_it_is_real(case, expected):
    ids = list(range(201, 230))
    obs = [search(ids[:5], candidates(ids, ids[:5]))]
    request = draft(ids[:5])
    reader = Reader()
    if case == "no_selector":
        request = dataclasses.replace(request, payload={})
    elif case == "models_pick":
        request = draft(ids[:3])
    elif case == "no_more_word":
        request = draft(ids[:5], more=None)
    elif case == "no_button_word":
        request = draft(ids[:5], button=None)
    elif case == "two_searches":
        others = list(range(301, 330))
        obs.append(search(others[:5], candidates(others, others[:5]), call_id="s2"))
        request = draft(ids[:3] + others[:2])
    elif case == "other_scope":
        obs = [search(ids[:5], candidates(ids, ids[:5], conversation_id=CONVERSATION + 1))]
    elif case == "nothing_more":
        obs = [search(ids[:5], candidates(ids[:5], ids[:5]))]
    elif case == "catalogue_unreadable":
        reader = Reader(fail=True)
    composed, reason = br.open_browse(request, obs, scope=SCOPE, runtime=runtime(reader),
                                      timeout_seconds=5)
    assert composed is None and reason == expected


# ── Eligibility is structure; the words are only words ─────────────────────


def _subsets(window: Sequence[int]) -> List[List[int]]:
    return [[pid for bit, pid in enumerate(window) if mask >> bit & 1]
            for mask in range(1, 1 << len(window))]


@pytest.mark.parametrize("more", ["More", None])
@pytest.mark.parametrize("button", ["Pick", None])
def test_whether_a_list_pages_never_depends_on_the_words(more, button):
    """The observation this closes: a missing ``more_label`` used to decide paging.

    Every selector over a search's window is judged the same with and without
    either word — the full window pages, every strict subset is the model's
    pick — and the words decide only which of them the list still needs.
    """
    ids = list(range(1301, 1330))
    obs = [search(ids[:5], candidates(ids, ids[:5]))]
    for chosen in _subsets(ids[:5]):
        with_words, reason_with = br.eligibility(draft(chosen), obs, scope=SCOPE,
                                                 search_tool_names=("search_products",))
        eligible, reason = br.eligibility(draft(chosen, more=more, button=button), obs, scope=SCOPE,
                                          search_tool_names=("search_products",))
        assert reason == reason_with
        assert (eligible is None) == (with_words is None)
        assert reason == (br.ELIGIBLE if len(chosen) == 5 else br.MODEL_PICK)
        if eligible is not None:
            expected = tuple(field for field, word in ((br.BUTTON_FIELD, button), (br.MORE_FIELD, more))
                             if word is None)
            assert eligible.words_missing(draft(chosen, more=more, button=button)) == expected


def test_a_selector_over_every_buyable_product_shown_is_the_searchs_results():
    """The model may leave out what cannot be bought; what it may not leave out
    is a product the customer could buy — that is a pick."""
    ids = list(range(1401, 1430))
    obs = [search(ids[:5], candidates(ids, ids[:5]), unorderable=[ids[3]])]
    buyable = [pid for pid in ids[:5] if pid != ids[3]]
    eligible, reason = br.eligibility(draft(buyable), obs, scope=SCOPE,
                                      search_tool_names=("search_products",))
    assert reason == br.ELIGIBLE and eligible.call_id == "s1"
    _none, reason = br.eligibility(draft(buyable[:3] + [ids[3]]), obs, scope=SCOPE,
                                   search_tool_names=("search_products",))
    assert reason == br.MODEL_PICK
    # A window with nothing the customer can buy offers no search to continue.
    obs = [search(ids[:5], candidates(ids, ids[:5]), unorderable=ids[:5])]
    _none, reason = br.eligibility(draft(ids[:5]), obs, scope=SCOPE,
                                   search_tool_names=("search_products",))
    assert reason == br.MODEL_PICK


def test_a_pick_is_never_extended_even_when_the_model_gives_the_more_word():
    ids = list(range(1501, 1530))
    reader = Reader()
    for chosen in ([ids[0]], ids[:2], [ids[1], ids[4]], ids[:4]):
        composed, reason = br.open_browse(draft(chosen), [search(ids[:5], candidates(ids, ids[:5]))],
                                          scope=SCOPE, runtime=runtime(reader), timeout_seconds=5)
        assert composed is None and reason == br.MODEL_PICK
    assert reader.asked == [], "a pick reads nothing from the catalogue"


def test_a_result_that_fits_one_list_needs_no_more_word():
    ids = list(range(1601, 1611))
    obs = [search(ids[:5], candidates(ids, ids[:5]))]
    eligible, _reason = br.eligibility(draft(ids[:5], more=None), obs, scope=SCOPE,
                                       search_tool_names=("search_products",))
    assert eligible.words_missing(draft(ids[:5], more=None)) == ()
    composed, reason = br.open_browse(draft(ids[:5], more=None), obs, scope=SCOPE,
                                      runtime=runtime(Reader()), timeout_seconds=5)
    assert reason == br.WHOLE and listed(composed.selection) == ids and not composed.plan


def test_the_words_request_names_fields_and_never_what_they_should_say():
    detail = br.words_needed_detail((br.BUTTON_FIELD, br.MORE_FIELD))
    assert br.BUTTON_FIELD in detail and br.MORE_FIELD in detail
    assert not any("\u0600" <= ch <= "\u06ff" for ch in detail), "no customer-language wording"


@dataclasses.dataclass
class _WordsSession:
    """The part of a loop session ``_paging_words_missing`` reads."""

    observations: List[Any]
    feedback: List[ac.VerificationFeedback] = dataclasses.field(default_factory=list)
    steps_remaining: bool = True
    seconds_left: float = 60.0
    limits: ac.LoopBudget = dataclasses.field(default_factory=ac.LoopBudget)
    events: List[Any] = dataclasses.field(default_factory=list)

    def steps_left(self) -> bool:
        return self.steps_remaining

    def remaining_seconds(self) -> float:
        return self.seconds_left

    def record(self, kind: str, detail: Mapping[str, Any]) -> None:
        self.events.append((kind, dict(detail)))


def _words_loop(*, browse: bool = True) -> al.AgentLoop:
    loop = al.AgentLoop.__new__(al.AgentLoop)
    loop._browse = runtime(Reader()) if browse else None
    loop._registry = type("Registry", (), {"definitions": ()})()
    return loop


@pytest.mark.parametrize("case,expected", [
    ("pages", (br.MORE_FIELD,)),
    ("no_browse_runtime", ()),
    ("already_asked_this_turn", ()),
    ("no_step_left", ()),
    ("no_time_for_a_step", ()),
    ("models_pick", ()),
    ("fits_one_list", ()),
    ("card_requested", ()),
])
def test_the_loop_asks_for_words_only_for_a_list_that_pages_and_only_once(case, expected):
    ids = list(range(1701, 1730))
    obs = [search(ids[:5], candidates(ids, ids[:5]))]
    request = draft(ids[:5], more=None)
    session = _WordsSession(observations=obs)
    loop = _words_loop(browse=case != "no_browse_runtime")
    if case == "already_asked_this_turn":
        # As a resumed invocation restores it: the code survives, the detail does not.
        session.feedback.append(ac.VerificationFeedback(
            step_no=2, problems=(ac.VerificationProblem(br.WORDS_NEEDED, "restored"),)))
    elif case == "no_step_left":
        session.steps_remaining = False
    elif case == "no_time_for_a_step":
        session.seconds_left = session.limits.provider_timeout_seconds + al.WORDS_RESERVE_SECONDS - 0.5
    elif case == "models_pick":
        request = draft(ids[:3], more=None)
    elif case == "fits_one_list":
        session.observations = [search(ids[:5], candidates(ids[:10], ids[:5]))]
    elif case == "card_requested":
        request = dataclasses.replace(request, payload={})
    assert loop._paging_words_missing(request, SCOPE, session, None) == expected


def test_a_capped_result_is_never_called_complete():
    ids = list(range(401, 451))
    composed, _reason = br.open_browse(
        draft(ids[:5]), [search(ids[:5], candidates(ids, ids[:5], complete=False))],
        scope=SCOPE, runtime=runtime(Reader()), timeout_seconds=5)
    assert composed.plan.mint.complete is False and composed.navigation["complete"] is False
    # Only what is stored can be shown: an incomplete result no longer than the
    # window — a general browse whose formatting window held few orderable
    # products — offers nothing more, and the model is not told that it does.
    assert not candidates(ids[:3], ids[:3], complete=False).extends_beyond_window
    assert candidates(ids[:6], ids[:5], complete=False).extends_beyond_window


def test_a_product_gone_since_the_search_is_counted_never_replaced():
    ids = list(range(501, 530))
    composed, _reason = br.open_browse(draft(ids[:5]), [search(ids[:5], candidates(ids, ids[:5]))],
                                       scope=SCOPE, runtime=runtime(Reader(gone=[ids[7]])),
                                       timeout_seconds=5)
    assert listed(composed.selection) == [pid for pid in ids[:9] if pid != ids[7]]
    assert composed.navigation["unavailable"] == 1
    assert composed.plan.mint.page_offset == 9, "the boundary does not move for a missing product"


def test_the_platform_never_lists_a_product_that_cannot_be_bought_now():
    """Hidden, archived and sold-out products stay off the rows the platform adds.

    The model may still name one it was shown flagged unorderable — its own
    judgement, as it always was — but the platform never adds one itself, and
    counts what it left out rather than hiding the gap.
    """
    ids = list(range(1101, 1130))
    reader = Reader(unorderable=[ids[6], ids[7]])
    obs = [search(ids[:5], candidates(ids, ids[:5]), unorderable=[ids[1]])]
    composed, reason = br.open_browse(draft(ids[:5]), obs, scope=SCOPE, runtime=runtime(reader),
                                      timeout_seconds=5)
    assert reason == br.OPENED
    assert listed(composed.selection) == [ids[0], ids[1], ids[2], ids[3], ids[4], ids[5], ids[8]]
    assert composed.navigation["unavailable"] == 2
    assert composed.row_refs == tuple(rc.product_ref(pid) for pid in listed(composed.selection))


def test_a_product_the_model_named_that_cannot_become_a_row_declines_the_browse():
    ids = list(range(1201, 1230))
    obs = [search(ids[:5], candidates(ids, ids[:5]))]
    obs[0] = dataclasses.replace(obs[0], result={**obs[0].result, "products": [
        {**row, "title": ""} if row["product_id"] == ids[2] else row
        for row in obs[0].result["products"]]})
    composed, reason = br.open_browse(draft(ids[:5]), obs, scope=SCOPE, runtime=runtime(Reader()),
                                      timeout_seconds=5)
    assert composed is None and reason == br.NAMED_NOT_LISTED


# ── Continuing a browse ──────────────────────────────────────────────────────


def _continuation(ids: Sequence[int], offset: int, *, complete: bool = True) -> nav.Continuation:
    return nav.Continuation(status=nav.RESOLVED, token="tapped", series="series-1",
                            product_ids=tuple(ids), page_offset=offset, complete=complete,
                            more_label="More", button_label="Pick", origin_turn_id=1,
                            origin_call_id="s1", search_method="fts", query_digest="d")


def test_a_later_page_spends_the_tapped_token_and_mints_the_next_in_the_same_series():
    ids = list(range(601, 631))
    page, facts = br.resolve_tap(peek=lambda: _continuation(ids, 9), read_rows=Reader(gone=[ids[12]]),
                                 scope=SCOPE, timeout_seconds=5)
    assert facts == {"navigation": "next_page_of_an_earlier_list", "status": nav.RESOLVED,
                     "page_number": 2, "products_on_this_page": 8, "no_longer_available": 1,
                     "more_pages_after_this": True, "more_matches_than_the_list_holds": False}
    composed = br.continue_browse(page)
    assert listed(composed.selection) == [pid for pid in ids[9:18] if pid != ids[12]]
    assert composed.plan.spend == "tapped"
    assert composed.plan.mint.series == "series-1" and composed.plan.mint.page_offset == 18
    (more,) = more_rows(composed.selection)
    assert more["title"] == "More" and composed.selection.button == "Pick"


def test_the_final_page_offers_no_more_and_a_capped_one_says_so():
    ids = list(range(701, 751))
    page, facts = br.resolve_tap(peek=lambda: _continuation(ids, 45, complete=False),
                                 read_rows=Reader(), scope=SCOPE, timeout_seconds=5)
    composed = br.continue_browse(page)
    assert listed(composed.selection) == ids[45:] and not more_rows(composed.selection)
    assert composed.plan.mint is None and composed.plan.spend == "tapped"
    assert facts["more_pages_after_this"] is False
    assert facts["more_matches_than_the_list_holds"] is True


def test_a_later_page_hidden_since_the_search_is_counted_and_never_listed():
    ids = list(range(1301, 1331))
    page, facts = br.resolve_tap(peek=lambda: _continuation(ids, 9),
                                 read_rows=Reader(unorderable=[ids[10], ids[11]]),
                                 scope=SCOPE, timeout_seconds=5)
    assert facts["products_on_this_page"] == 7 and facts["no_longer_available"] == 2
    assert ids[10] not in listed(br.continue_browse(page).selection)


def test_a_page_with_nothing_left_and_nothing_after_goes_as_text_and_is_still_spent():
    ids = list(range(1401, 1420))      # nineteen: page two is the last
    page, facts = br.resolve_tap(peek=lambda: _continuation(ids, 9), read_rows=Reader(gone=ids[9:]),
                                 scope=SCOPE, timeout_seconds=5)
    composed = br.continue_browse(page)
    assert facts["products_on_this_page"] == 0 and facts["more_pages_after_this"] is False
    assert composed.selection is None and composed.reason == br.PAGE_EMPTY
    assert composed.plan.spend == "tapped" and composed.plan.mint is None


@pytest.mark.parametrize("status", [nav.NOT_FOUND, nav.REPLAYED, nav.EXPIRED, nav.UNAVAILABLE,
                                    nav.SPENT_BY_THIS_TURN])
def test_a_refused_tap_is_a_named_fact_and_no_page_and_reads_nothing(status):
    reader = Reader()
    page, facts = br.resolve_tap(peek=lambda: nav.Continuation(status=status), read_rows=reader,
                                 scope=SCOPE, timeout_seconds=5)
    assert page is None and facts == br.refusal_facts(status) and reader.asked == []


# ── Precedence ───────────────────────────────────────────────────────────────


def _page() -> br.BrowsePage:
    ids = list(range(801, 831))
    return br.BrowsePage(continuation=_continuation(ids, 9),
                         products=tuple(product(i) for i in ids[9:18]), missing=())


def test_a_verified_more_tap_is_the_shape_and_a_requested_selector_stands_down():
    shape = pp.decide(draft=draft([1, 2]), observations=(), definitions=(),
                      presentation=pp.PresentationContext(browse_page=_page()))
    assert (shape.kind, shape.reason, shape.withhold_selector) == (pp.SHAPE_LIST, pp.NAVIGATION_PAGE, True)
    assert shape.determined_product_id is None, "navigation never selects a product"


def test_a_product_tap_still_comes_first():
    shape = pp.decide(draft=draft([1, 2]), observations=(), definitions=(),
                      presentation=pp.PresentationContext(tapped_product_id=5,
                                                          browse_page=_page()))
    assert shape.reason == pp.TAP_SELECTED


def test_without_a_browse_runtime_the_loop_shapes_exactly_as_before():
    """A loop that was handed no browse runtime has no path to paging at all."""
    assert al.AgentLoop._browse is None
    ids = list(range(901, 930))
    loop = al.AgentLoop.__new__(al.AgentLoop)
    loop._registry = at.ToolRegistry(())
    session = al._Session(scope=al._Scope(TENANT, "live", CONVERSATION, TURN),
                          token=c.OwnershipToken(owner_id="t", fence=1, epoch=1, tenant_id=TENANT,
                                                 namespace="live", conversation_id=CONVERSATION),
                          requested=ac.LoopBudget(), clock=time.monotonic)
    session.observations = [search(ids[:5], candidates(ids, ids[:5]))]
    shaped = loop._shape_reply(draft(ids[:5]), SCOPE, session, None)
    assert [rc.product_id_from_row_id(r["id"]) for r in shaped.payload[rc.CHOICES_KEY]["rows"]] == ids[:5]
    assert not session.navigation_plan


# ── Provenance and ownership of the substrate ────────────────────────────────


def test_the_lifted_schema_comparison_is_0111s_own_but_for_the_foreign_key_fix():
    """``runtime_schema_guarantees`` claims to be 0111's comparison, lifted, with
    one correction. This pins that claim: every function they share is
    identical, except the one the correction names."""
    def functions(path: Path) -> Dict[str, str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {node.name: ast.unparse(node) for node in tree.body
                if isinstance(node, ast.FunctionDef)}

    applied = functions(REPO_ROOT / "database/migrations/versions/0111_commerce_runtime_handover.py")
    lifted = functions(REPO_ROOT / "database/runtime_schema_guarantees.py")
    differing = {name for name in set(applied) & set(lifted) if applied[name] != lifted[name]}
    assert differing == {"_reference_guarantees"}
    assert set(lifted) - set(applied) == {"_referred_tables"}


def test_cleanup_has_an_owner_the_application_starts():
    source = (REPO_ROOT / "backend/main.py").read_text(encoding="utf-8")
    assert 'from core.commerce_runtime.navigation import run_navigation_sweep_scheduler' in source
    assert '_start("commerce_runtime_navigation_sweep", _f_navigation_sweep' in source


def test_the_sweep_is_registered_unconditionally_beside_every_other_scheduler():
    """Where in startup the registration sits, not only that its text exists.

    It is a direct statement of the same body that registers every other
    scheduler, after the one switch that disables them all
    (``NAHLA_DISABLE_SCHEDULERS``), under no condition of its own; and the
    factory it queues calls the loop with no arguments — the application's
    engine and the production intervals.
    """
    tree = ast.parse((REPO_ROOT / "backend/main.py").read_text(encoding="utf-8"))
    owner = next(node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and any(isinstance(inner, ast.FunctionDef) and inner.name == "_start"
                         for inner in node.body))

    def starts(statement: ast.stmt) -> str:
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                and getattr(statement.value.func, "id", "") == "_start"
                and statement.value.args and isinstance(statement.value.args[0], ast.Constant)):
            return str(statement.value.args[0].value)
        return ""

    positions = {starts(statement): index for index, statement in enumerate(owner.body)
                 if starts(statement)}
    switch = next(index for index, statement in enumerate(owner.body)
                  if isinstance(statement, ast.If)
                  and getattr(statement.test, "id", "") == "_skip_schedulers")
    assert "commerce_runtime_navigation_sweep" in positions, "registered under a condition of its own"
    assert positions["commerce_runtime_navigation_sweep"] > switch
    assert "webhook_dispatcher" in positions, "the other schedulers are registered in the same body"
    factory = next(statement for statement in owner.body
                   if isinstance(statement, ast.FunctionDef) and statement.name == "_f_navigation_sweep")
    (returned,) = [node for node in ast.walk(factory) if isinstance(node, ast.Return)]
    assert isinstance(returned.value, ast.Call)
    assert returned.value.func.id == "run_navigation_sweep_scheduler"
    assert not returned.value.args and not returned.value.keywords
