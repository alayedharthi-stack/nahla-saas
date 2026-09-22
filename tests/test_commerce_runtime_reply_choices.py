"""The model offers the selector; the platform composes what each row says.

A tappable list is the affordance the pilot was missing: the customer was shown
five dresses as prose and had no way to say which one. Here the model decides
whether a selector helps and names the products by id; every value the customer
then reads comes from this turn's own tool results. A product it names without
looking up is refused, and anything that merely will not fit the channel
degrades to the model's text with every option still in it.

Offline and merchant-agnostic: dresses (Tenant 1's own shape), and generic
shirts, shoes and perfume.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import ledger_contracts as lc  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402


def product(product_id: int, title: str, **fields: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {"product_id": product_id, "title": title, "currency": "SAR",
                           "evidence_ref": rc.product_ref(product_id)}
    row.update(fields)
    return row


def search(*products: Dict[str, Any], call_id: str = "c1", ok: bool = True) -> ac.ToolObservation:
    return ac.ToolObservation(
        call_id=call_id, tool_name="search_products", ok=ok,
        result={"status": "ok", "found": bool(products), "products": list(products)} if ok else None,
        error_code=None if ok else "no_evidence", error=None,
        evidence_refs=tuple(rc.product_ref(p["product_id"]) for p in products) if ok else (),
    )


def lookup(one: Dict[str, Any], call_id: str = "c2") -> ac.ToolObservation:
    return ac.ToolObservation(
        call_id=call_id, tool_name="get_product_details", ok=True,
        result={"status": "ok", "found": True, "product": one},
        error_code=None, error=None, evidence_refs=(rc.product_ref(one["product_id"]),),
    )


def draft(product_ids: Optional[Sequence[int]] = None, *, cite: Optional[Sequence[int]] = None,
          button: str = "", text: str = "عندنا هالخيارات") -> ac.ReplyDraft:
    payload: Dict[str, Any] = {}
    if product_ids is not None:
        request: Dict[str, Any] = {"product_ids": list(product_ids)}
        if button:
            request["button"] = button
        payload[rc.REQUESTED_KEY] = request
    cited = product_ids if cite is None else cite
    return ac.ReplyDraft(text=text, evidence_refs=tuple(rc.product_ref(i) for i in (cited or ())),
                         claims_commerce_facts=True, payload=payload)


DRESSES = [product(23, "فستان", price="289.0", sale_price="144.0"),
           product(37, "فستان", price="229.0", sale_price="114.0"),
           product(38, "فستان", price="279.0", sale_price="83.0")]


# ── What a row says comes from the merchant's records ────────────────────────

def test_the_rows_are_composed_from_this_turns_observations_not_the_models_words() -> None:
    observations = [search(*DRESSES)]
    final, reason = rc.finalize(draft([23, 37, 38]), observations)
    assert reason == rc.OFFERED and final.kind == lc.DeliveryKind.RICH.value
    rows, _button = rc.payload_rows(final.payload)
    assert [row["title"] for row in rows] == ["فستان · 144.0 SAR", "فستان · 114.0 SAR",
                                              "فستان · 83.0 SAR"]
    assert [row["id"] for row in rows] == ["nahla:choice:23", "nahla:choice:37",
                                           "nahla:choice:38"]


def test_the_text_the_model_wrote_is_carried_through_untouched() -> None:
    written = "تحت أمرك، هذي الفساتين المتوفرة الحين"
    final, _reason = rc.finalize(draft([23, 37], text=written), [search(*DRESSES)])
    assert final.text == written
    assert final.evidence_refs == ("catalog:product:23", "catalog:product:37")


def test_the_rows_keep_the_order_the_model_asked_for() -> None:
    final, _reason = rc.finalize(draft([38, 23]), [search(*DRESSES)])
    rows, _ = rc.payload_rows(final.payload)
    assert [row["id"] for row in rows] == ["nahla:choice:38", "nahla:choice:23"]


def test_a_generic_catalogue_is_offered_the_same_way() -> None:
    catalogue = [product(11, "قميص قطني أزرق", price="129.0"),
                 product(12, "حذاء رياضي أبيض", price="310.0"),
                 product(13, "عطر ورد 100ml", price="240.0", sale_price="199.0")]
    final, reason = rc.finalize(draft([11, 12, 13]), [search(*catalogue)])
    assert reason == rc.OFFERED
    rows, _ = rc.payload_rows(final.payload)
    assert [row["title"] for row in rows] == ["قميص قطني أزرق", "حذاء رياضي أبيض", "عطر ورد 100ml"]
    assert rows[2]["description"] == "199.0 SAR"


def test_the_button_is_the_models_word_and_the_platform_supplies_none() -> None:
    final, _reason = rc.finalize(draft([23, 37], button="اختاري"), [search(*DRESSES)])
    _rows, button = rc.payload_rows(final.payload)
    assert button == "اختاري"

    bare, _reason = rc.finalize(draft([23, 37]), [search(*DRESSES)])
    _rows, no_button = rc.payload_rows(bare.payload)
    assert no_button == ""


def test_an_overlong_button_word_is_bounded_to_what_the_channel_shows() -> None:
    final, _reason = rc.finalize(draft([23, 37], button="ا" * 60), [search(*DRESSES)])
    _rows, button = rc.payload_rows(final.payload)
    assert len(button) == rc.MAX_BUTTON_LABEL


# ── Evidence: a row states a price, so it needs this turn's evidence ─────────

def test_a_product_not_looked_up_in_this_turn_is_never_offered() -> None:
    problems = ac.verify_reply_draft(draft([23, 99]), [search(*DRESSES)])
    # Two independent statements about the same product, both true: the
    # reference was never observed, and the row would price it anyway.
    assert [p.code for p in problems] == ["unknown_evidence", "choice_without_evidence"]
    assert all("99" in p.detail for p in problems)


def test_a_product_looked_up_but_not_cited_is_never_offered() -> None:
    problems = ac.verify_reply_draft(draft([23, 37], cite=[23]), [search(*DRESSES)])
    assert [p.code for p in problems] == ["choice_without_evidence"]


def test_a_failed_lookup_is_not_evidence_for_a_row() -> None:
    problems = ac.verify_reply_draft(draft([23, 37]), [search(*DRESSES, ok=False)])
    assert {p.code for p in problems} == {"unknown_evidence", "choice_without_evidence"}


def test_an_offered_set_that_is_fully_observed_and_cited_verifies() -> None:
    assert ac.verify_reply_draft(draft([23, 37, 38]), [search(*DRESSES)]) == ()


def test_a_reply_with_no_selector_is_verified_exactly_as_before() -> None:
    plain = ac.ReplyDraft(text="أهلاً بك", claims_commerce_facts=False)
    assert ac.verify_reply_draft(plain, []) == ()


# ── Nothing real is dropped to make a list fit ───────────────────────────────

def test_more_options_than_the_channel_shows_are_sent_as_text_never_trimmed() -> None:
    many = [product(100 + i, f"قميص قطني أزرق {i}", price=f"{100 + i}.0") for i in range(12)]
    ids = [p["product_id"] for p in many]
    assert ac.verify_reply_draft(draft(ids), [search(*many)]) == ()
    final, reason = rc.finalize(draft(ids), [search(*many)])
    assert reason == rc.TOO_MANY
    assert final.kind == lc.DeliveryKind.TEXT.value and rc.payload_rows(final.payload) == ([], "")


def test_one_option_is_not_a_choice_and_the_text_already_names_it() -> None:
    final, reason = rc.finalize(draft([23]), [search(*DRESSES)])
    assert reason == rc.TOO_FEW and final.kind == lc.DeliveryKind.TEXT.value


def test_a_product_with_no_usable_title_withholds_the_whole_selector() -> None:
    catalogue = [product(11, "قميص قطني أزرق", price="129.0"), product(12, "   ", price="149.0")]
    final, reason = rc.finalize(draft([11, 12]), [search(*catalogue)])
    assert reason == rc.INCOMPLETE and final.kind == lc.DeliveryKind.TEXT.value


def test_products_no_fact_separates_are_numbered_and_still_offered() -> None:
    same = [product(1, "فستان", price="199.0"), product(2, "فستان", price="199.0")]
    final, reason = rc.finalize(draft([1, 2]), [search(*same)])
    assert reason == rc.OFFERED
    rows, _ = rc.payload_rows(final.payload)
    assert [row["title"] for row in rows] == ["فستان · 1", "فستان · 2"]


def test_a_reply_that_asked_for_no_selector_stays_plain_text() -> None:
    final, reason = rc.finalize(draft(), [search(*DRESSES)])
    assert reason == rc.NOT_REQUESTED
    assert final.kind == lc.DeliveryKind.TEXT.value and final.payload == {}


# ── Reading the turn's observations ──────────────────────────────────────────

def test_both_a_search_and_a_single_lookup_are_read() -> None:
    observed = rc.observed_products([search(DRESSES[0]), lookup(DRESSES[1])])
    assert sorted(observed) == [23, 37]


def test_the_later_read_of_the_same_product_is_the_one_kept() -> None:
    stale = product(37, "فستان", price="229.0")
    fresh = product(37, "فستان", price="114.0")
    observed = rc.observed_products([search(stale), lookup(fresh)])
    assert observed[37]["price"] == "114.0"


def test_an_observation_whose_body_was_dropped_offers_nothing() -> None:
    truncated = ac.ToolObservation(
        call_id="c1", tool_name="search_products", ok=True,
        result={"status": "ok", "products": [DRESSES[0]]}, error_code=None, error=None,
        evidence_refs=(rc.product_ref(23),), restored=True, body_truncated=True)
    assert rc.observed_products([truncated]) == {}
    problems = ac.verify_reply_draft(draft([23, 37]), [truncated])
    assert [p.code for p in problems if p.code == "choice_without_evidence"] == [
        "choice_without_evidence"] * 2


# ── A tap is a claim until it verifies ───────────────────────────────────────

def test_a_row_id_the_platform_minted_resolves_to_its_product() -> None:
    assert rc.product_id_from_row_id(rc.row_id(37)) == 37


def test_anything_else_on_that_field_resolves_to_no_product() -> None:
    for value in ("", None, "37", "order:summary:4", "nahla:choice:", "nahla:choice:abc",
                  "nahla:choice:-3", "nahla:choice:0", "some_other_surface_row"):
        assert rc.product_id_from_row_id(value) is None


# ── The bounded recovery keeps the answer, loses only the tapping ────────────

def test_the_same_reply_without_its_selector_keeps_text_and_evidence() -> None:
    stored = {"text": "هذي الخيارات", "evidence_refs": ["catalog:product:23"],
              rc.CHOICES_KEY: {"rows": [{"id": "nahla:choice:23", "title": "فستان"}]}}
    assert rc.text_only_payload(stored) == {"text": "هذي الخيارات",
                                            "evidence_refs": ["catalog:product:23"]}


def test_a_stored_payload_without_a_selector_reads_as_no_rows() -> None:
    assert rc.payload_rows({"text": "أهلاً"}) == ([], "")
    assert rc.payload_rows({rc.CHOICES_KEY: {"rows": "not a list"}}) == ([], "")


# ── Malformed requests never become a selector ───────────────────────────────

def test_ids_that_are_not_products_are_ignored_rather_than_guessed() -> None:
    messy = ac.ReplyDraft(text="…", claims_commerce_facts=True,
                          payload={rc.REQUESTED_KEY: {"product_ids": [23, "37", 0, -1, None,
                                                                      True, 23, 38.0]}})
    assert rc.requested_product_ids(messy) == (23, 37, 38)


def test_a_request_shaped_wrongly_asks_for_nothing() -> None:
    for payload in ({}, {rc.REQUESTED_KEY: {}}, {rc.REQUESTED_KEY: {"product_ids": "23"}},
                    {rc.REQUESTED_KEY: []}):
        assert rc.requested_product_ids(ac.ReplyDraft(text="…", payload=payload)) == ()


def test_the_verified_payload_survives_the_platforms_own_payload_validation() -> None:
    final, _reason = rc.finalize(draft([23, 37, 38], button="اختر"), [search(*DRESSES)])
    validated = ac.validate_reply_draft(final)
    assert validated.kind == lc.DeliveryKind.RICH.value
    rows, button = rc.payload_rows(validated.payload)
    assert len(rows) == 3 and button == "اختر"
