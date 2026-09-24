"""Presentation is the platform's; content stays the agent's.

Fifteen acceptance cases over the shape a commerce reply takes — a tappable
List, a Product Card, or plain Text — plus the negative controls that prove each
guarantee is carried by the code and not by the test.

Everything runs through the **production seam**: ``AgentLoop._shape_reply``, the
one method ``_reason`` calls after verification passes, driven with a real
``ToolRegistry``, real ``ToolObservation``s and a real loop session. Hydration
goes through ``ToolRegistry.execute`` exactly as a model's own call does, so the
tenant scope, the schema validation and the bounded execution are the real ones.

Merchant-agnostic by construction: a generic demo store whose catalogue rotates
categories — a cotton shirt, a white sneaker, a rose perfume, a leather belt —
and a second tenant that must never appear in the first one's answers. No
product name, category or customer phrase decides anything here, because the
policy cannot read any of them.

No model is called, no database is touched, nothing is ever sent.
"""
from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import agent_loop as al  # noqa: E402
from core.commerce_runtime import agent_tools as at  # noqa: E402
from core.commerce_runtime import contracts as c  # noqa: E402
from core.commerce_runtime import ledger_contracts as lc  # noqa: E402
from core.commerce_runtime import presentation_policy as pp  # noqa: E402
from core.commerce_runtime import reply_card as rcard  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402

TENANT = 7
OTHER_TENANT = 8
CONVERSATION = 91
TURN = 4021

IMAGE = "https://cdn.example.test/generic/{0}.jpg"
LINK = "https://demostore.example.test/p/{0}"


# ── A generic merchant, rotating categories ──────────────────────────────────

def _row(product_id: int, title: str, price: str, **fields: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "product_id": product_id, "title": title, "price": price, "currency": "SAR",
        "in_stock": True, "evidence_ref": rc.product_ref(product_id),
        "image_url": IMAGE.format(product_id), "product_url": LINK.format(product_id),
    }
    row.update(fields)
    return row


CATALOGUE: Dict[int, Dict[int, Dict[str, Any]]] = {
    TENANT: {
        11: _row(11, "قميص قطني أزرق", "149.00"),
        12: _row(12, "حذاء رياضي أبيض", "299.00"),
        13: _row(13, "عطر ورد 100ml", "420.00"),
        14: _row(14, "حزام جلد بني", "99.00"),
        # A product the merchant has never photographed: a real, common shape.
        15: _row(15, "ساعة يد كلاسيكية", "540.00", image_url=""),
    },
    OTHER_TENANT: {21: _row(21, "شماغ صيفي", "210.00")},
}


# ── A registry whose tools carry the production result kinds ─────────────────

def _product_details(scope: at.ToolScope, arguments: Mapping[str, Any]) -> at.ToolResult:
    """The merchant's own row, read now, for this tenant only."""
    product = CATALOGUE.get(int(scope.tenant_id), {}).get(int(arguments["product_id"]))
    if product is None:
        return at.ToolResult(result={"status": "not_found", "found": False,
                                     "reason": "product_not_found_in_tenant_catalog"},
                             evidence_refs=())
    return at.ToolResult(result={"status": "ok", "found": True, "product": dict(product)},
                         evidence_refs=(str(product["evidence_ref"]),))


def _unreadable(scope: at.ToolScope, arguments: Mapping[str, Any]) -> at.ToolResult:
    raise RuntimeError("the catalogue could not be read")


def _search(scope: at.ToolScope, arguments: Mapping[str, Any]) -> at.ToolResult:
    wanted = [int(v) for v in (arguments.get("ids") or ())]
    rows = [dict(CATALOGUE[int(scope.tenant_id)][i]) for i in wanted
            if i in CATALOGUE.get(int(scope.tenant_id), {})]
    return at.ToolResult(result={"status": "ok", "found": bool(rows), "products": rows},
                         evidence_refs=tuple(str(r["evidence_ref"]) for r in rows))


def _order_details(scope: at.ToolScope, arguments: Mapping[str, Any]) -> at.ToolResult:
    """An order carries products *incidentally*: line items, not a browse."""
    return at.ToolResult(
        result={"status": "ok", "found": True,
                "order": {"order_id": 500, "total": "448.00",
                          "items": [{"product_id": 11, "title": "قميص قطني أزرق", "quantity": 2},
                                    {"product_id": 13, "title": "عطر ورد 100ml", "quantity": 1}]},
                # Deliberately the same shape a catalogue read returns, so the
                # test proves the *declared kind* is what excludes it and not
                # the absence of a recognisable field.
                "products": [dict(CATALOGUE[TENANT][11]), dict(CATALOGUE[TENANT][13])]},
        evidence_refs=("order:500",))


def _definition(name: str, kind: str, schema: Mapping[str, Any]) -> ac.ToolDefinition:
    return ac.ToolDefinition(name=name, description=f"{name} (fixture)", input_schema=schema,
                             result_kind=kind)


_ID = {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}
_IDS = {"type": "array", "items": _ID, "maxItems": 20}


def build_registry(*, details=_product_details) -> at.ToolRegistry:
    return at.ToolRegistry((
        at.RegisteredTool(
            _definition("get_product_details", pp.FOCUS_KIND,
                        {"type": "object", "additionalProperties": False,
                         "properties": {"product_id": _ID}, "required": ["product_id"]}),
            details),
        at.RegisteredTool(
            _definition("search_products", pp.CANDIDATE_KIND,
                        {"type": "object", "additionalProperties": False,
                         "properties": {"ids": _IDS}, "required": ["ids"]}),
            _search),
        at.RegisteredTool(
            _definition("get_order_details", "order_summary",
                        {"type": "object", "additionalProperties": False,
                         "properties": {"order_id": _ID}, "required": ["order_id"]}),
            _order_details),
    ))


# ── The production seam, driven directly ─────────────────────────────────────

def _session() -> "al._Session":
    return al._Session(scope=al._Scope(TENANT, "live", CONVERSATION, TURN),
                       token=c.OwnershipToken(owner_id="presentation-test", fence=1, epoch=1,
                                              tenant_id=TENANT, namespace="live",
                                              conversation_id=CONVERSATION),
                       requested=ac.LoopBudget(), clock=time.monotonic)


def _scope(tenant: int = TENANT) -> at.ToolScope:
    return at.ToolScope(tenant_id=tenant, namespace="live", conversation_id=CONVERSATION, turn_id=TURN)


def run_seam(draft: ac.ReplyDraft, observations: Sequence[ac.ToolObservation], *,
             presentation: Optional[pp.PresentationContext] = None,
             registry: Optional[at.ToolRegistry] = None,
             tenant: int = TENANT) -> Tuple[ac.ReplyDraft, "al._Session"]:
    """``AgentLoop._shape_reply`` itself — the method ``_reason`` calls."""
    loop = al.AgentLoop.__new__(al.AgentLoop)
    loop._registry = registry or build_registry()
    session = _session()
    session.observations = list(observations)
    final = loop._shape_reply(draft, _scope(tenant), session, presentation)
    return final, session


def observed(tool: str, result: Mapping[str, Any], refs: Sequence[str] = (),
             *, call_id: str = "m1", ok: bool = True) -> ac.ToolObservation:
    return ac.ToolObservation(call_id=call_id, tool_name=tool, ok=ok, result=dict(result),
                              error_code=None, error=None, evidence_refs=tuple(refs))


def read(product_id: int, tenant: int = TENANT, call_id: str = "m1") -> ac.ToolObservation:
    row = CATALOGUE[tenant][product_id]
    return observed("get_product_details", {"status": "ok", "found": True, "product": dict(row)},
                    (str(row["evidence_ref"]),), call_id=call_id)


def browsed(*product_ids: int, tenant: int = TENANT) -> ac.ToolObservation:
    rows = [dict(CATALOGUE[tenant][i]) for i in product_ids]
    return observed("search_products", {"status": "ok", "found": True, "products": rows},
                    tuple(str(r["evidence_ref"]) for r in rows))


def draft(text: str = "تفضل", *, cite: Sequence[int] = (), payload: Optional[Dict[str, Any]] = None,
          extra_refs: Sequence[str] = ()) -> ac.ReplyDraft:
    refs = tuple(rc.product_ref(i) for i in cite) + tuple(extra_refs)
    return ac.ReplyDraft(text=text, evidence_refs=refs, claims_commerce_facts=True,
                         payload=dict(payload or {}))


def shape_of(session: "al._Session") -> Dict[str, Any]:
    """What the seam recorded about the shape it chose, from the loop's own event."""
    for event in reversed(session.events):
        if event.kind == "reply_accepted":
            return dict(event.detail)
    raise AssertionError("the seam recorded no accepted reply")


def card_of(final: ac.ReplyDraft) -> Optional[Dict[str, Any]]:
    return rcard.payload_card(final.payload)


# ══ 1. A browse of several products is a List ════════════════════════════════

def test_1_several_candidates_and_no_focus_become_a_list() -> None:
    request = {rc.REQUESTED_KEY: {"product_ids": [11, 12, 13], "button": "اختر"}}
    final, session = run_seam(draft(cite=[11, 12, 13], payload=request), [browsed(11, 12, 13)])
    rows, _button = rc.payload_rows(final.payload)
    assert [r["id"] for r in rows] == [rc.row_id(i) for i in (11, 12, 13)]
    assert final.kind == lc.DeliveryKind.RICH.value and card_of(final) is None
    assert shape_of(session)["shape"] == pp.SHAPE_LIST


def test_1b_the_policy_reaches_a_list_on_its_own_when_the_model_offers_nothing() -> None:
    """The shape does not depend on the model asking: three candidates, no focus."""
    decided = pp.decide(draft=draft(cite=[11, 12, 13]), observations=[browsed(11, 12, 13)],
                        definitions=build_registry().definitions)
    assert (decided.kind, decided.reason) == (pp.SHAPE_LIST, pp.MULTIPLE_CANDIDATES)


# ══ 2. One search candidate is not a selection ═══════════════════════════════

def test_2_a_single_search_candidate_never_becomes_a_card() -> None:
    """A product that is alone in a result is a candidate, not a choice."""
    decided = pp.decide(draft=draft(cite=[12]), observations=[browsed(12)],
                        definitions=build_registry().definitions)
    assert (decided.kind, decided.reason) == (pp.SHAPE_TEXT, pp.NO_PRODUCT_FOCUS)

    final, session = run_seam(draft(cite=[12]), [browsed(12)])
    assert card_of(final) is None and final.kind != lc.DeliveryKind.RICH.value
    assert shape_of(session)["shape"] == pp.SHAPE_TEXT


# ══ 3. A verified tap leads to a Card, deterministically ═════════════════════

def test_3_a_verified_tap_hydrates_and_becomes_a_card_with_no_model_product_call() -> None:
    """The acceptance case. The model made **no** product call this turn; the
    platform read the tapped product itself and the card is composed from it."""
    tap = pp.PresentationContext(tapped_product_id=12)
    final, session = run_seam(draft("وصلني"), [], presentation=tap)

    card = card_of(final)
    assert card is not None, "a verified tap must produce a card without the model's help"
    assert card["product_id"] == 12
    assert card["image_url"] == IMAGE.format(12) and card["button_url"] == LINK.format(12)
    assert final.kind == lc.DeliveryKind.RICH.value
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_CARD, pp.TAP_SELECTED)
    # The read really happened, through the registry, and is in the turn's
    # observations with the merchant's own evidence reference.
    assert any(o.call_id == pp.HYDRATION_CALL_ID and o.ok for o in session.observations)
    assert rc.product_ref(12) in {ref for o in session.observations for ref in o.evidence_refs}


def test_3_negative_control_removing_the_hydration_breaks_the_card(monkeypatch) -> None:
    """The guarantee is carried by the hydration, not by the test's setup.

    With the platform's own read taken away — and the model still making no
    product call — the very same turn produces no card at all. This is the
    control for the case above: it fails if hydration is ever dropped.
    """
    monkeypatch.setattr(pp, "hydration_request", lambda shape: None)
    tap = pp.PresentationContext(tapped_product_id=12)
    final, session = run_seam(draft("وصلني"), [], presentation=tap)

    assert card_of(final) is None
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_TEXT,
                                                             pp.TAP_HYDRATION_UNAVAILABLE)
    assert not any(o.call_id == pp.HYDRATION_CALL_ID for o in session.observations)


def test_3b_a_tap_the_model_already_looked_up_is_not_read_twice() -> None:
    """A deliberate read in this same turn went through the same contract, so
    it is exactly as fresh. What must never happen is the card *depending* on
    the model having made it — which the case above proves it does not."""
    tap = pp.PresentationContext(tapped_product_id=13)
    final, session = run_seam(draft(cite=[13]), [read(13)], presentation=tap)
    assert (card_of(final) or {})["product_id"] == 13
    assert not any(o.call_id == pp.HYDRATION_CALL_ID for o in session.observations)


# ══ 4. A tapped product that is gone fails closed, with a name ═══════════════

def test_4_a_tapped_product_no_longer_in_the_catalogue_falls_back_to_text() -> None:
    tap = pp.PresentationContext(tapped_product_id=999)
    final, session = run_seam(draft("تفضل"), [], presentation=tap)
    assert card_of(final) is None
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_TEXT, pp.TAP_PRODUCT_NOT_FOUND)
    # The reason reaches the payload production persists, not only the log.
    assert final.payload.get(rcard.WITHHELD_KEY) == pp.TAP_PRODUCT_NOT_FOUND


def test_4b_another_tenants_product_is_not_found_here() -> None:
    """Tenant isolation is the tool's, not the policy's: the read is scoped."""
    tap = pp.PresentationContext(tapped_product_id=21)       # the other merchant's product
    final, session = run_seam(draft("تفضل"), [], presentation=tap)
    assert card_of(final) is None
    assert shape_of(session)["shape_reason"] == pp.TAP_PRODUCT_NOT_FOUND


# ══ 5. A read that could not be made is not a product that is gone ═══════════

def test_5_an_unreadable_catalogue_is_reported_as_unavailable_not_as_missing() -> None:
    tap = pp.PresentationContext(tapped_product_id=12)
    final, session = run_seam(draft("تفضل"), [], presentation=tap,
                              registry=build_registry(details=_unreadable))
    assert card_of(final) is None
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_TEXT,
                                                             pp.TAP_HYDRATION_UNAVAILABLE)
    # "could not be read" and "is not there" are different facts to a merchant,
    # and both reach the persisted payload rather than only a log line.
    assert final.payload.get(rcard.WITHHELD_KEY) == pp.TAP_HYDRATION_UNAVAILABLE


# ══ 6. A merchant's own missing photo withholds the card, not the answer ═════

def test_6_a_tapped_product_with_no_photo_keeps_the_answer_and_names_the_limit() -> None:
    written = "الساعة الكلاسيكية متوفرة بسعر 540 ريال."
    tap = pp.PresentationContext(tapped_product_id=15)       # no image_url
    final, _session = run_seam(draft(written), [], presentation=tap)
    assert card_of(final) is None
    assert final.payload.get(rcard.WITHHELD_KEY) == rcard.NO_IMAGE
    assert final.text == written


# ══ 7. One deliberate lookup is a focus ══════════════════════════════════════

def test_7_a_single_deliberate_product_read_becomes_a_card() -> None:
    final, session = run_seam(draft(cite=[14]), [read(14)])
    assert (card_of(final) or {})["product_id"] == 14
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_CARD, pp.FOCUSED_PRODUCT)


# ══ 8. Two deliberate lookups are a comparison, not a focus ══════════════════

def test_8_two_deliberate_reads_are_a_comparison_and_never_one_products_card() -> None:
    decided = pp.decide(draft=draft(cite=[11, 12]), observations=[read(11, call_id="m1"),
                                                                 read(12, call_id="m2")],
                        definitions=build_registry().definitions)
    assert (decided.kind, decided.reason) == (pp.SHAPE_LIST, pp.MULTIPLE_CANDIDATES)

    final, _session = run_seam(draft(cite=[11, 12]), [read(11, call_id="m1"), read(12, call_id="m2")])
    assert card_of(final) is None


# ══ 9. Products that appear incidentally drive nothing ═══════════════════════

def test_9_products_inside_an_order_are_facts_not_a_browse() -> None:
    """The order result carries a ``products`` list of the same shape a catalogue
    read returns. It is excluded by the tool's **declared kind**, so a tool added
    tomorrow is excluded the same way without anyone listing it."""
    order = observed("get_order_details", _order_details(_scope(), {"order_id": 500}).result,
                     ("order:500",))
    reads = pp.provenance([order], build_registry().definitions)
    assert reads.candidates == () and reads.focused == ()

    final, session = run_seam(draft("طلبك يحتوي صنفين.", extra_refs=("order:500",)), [order])
    assert card_of(final) is None and rc.payload_rows(final.payload)[0] == []
    assert shape_of(session)["shape"] == pp.SHAPE_TEXT


# ══ 10. The same card is not sent twice when nothing new was selected ════════

def test_10_a_delivered_card_suppresses_the_same_product_next_turn() -> None:
    already = pp.PresentationContext(last_card_product_id=14)
    final, session = run_seam(draft(cite=[14]), [read(14)], presentation=already)
    assert card_of(final) is None
    recorded = shape_of(session)
    assert (recorded["shape"], recorded["shape_reason"]) == (pp.SHAPE_TEXT,
                                                             pp.RECENT_CARD_SUPPRESSED)


def test_10b_suppression_is_per_product_not_per_conversation() -> None:
    already = pp.PresentationContext(last_card_product_id=14)
    final, _session = run_seam(draft(cite=[13]), [read(13)], presentation=already)
    assert (card_of(final) or {})["product_id"] == 13


# ══ 11. Suppression rests on a delivered card, never on a payload ════════════

def test_11_a_card_that_was_never_delivered_suppresses_nothing() -> None:
    """``last_card_product_id`` is written only from an accepted, identified send
    (``commerce_runtime_pilot._record`` returns early otherwise, and the turn
    report clears it when the bounded recovery replaced the send). A reserved
    payload that carried a card the provider refused therefore leaves it unset,
    and the next turn's card is not suppressed."""
    final, session = run_seam(draft(cite=[14]), [read(14)],
                              presentation=pp.PresentationContext(last_card_product_id=None))
    assert (card_of(final) or {})["product_id"] == 14
    assert shape_of(session)["shape_reason"] == pp.FOCUSED_PRODUCT


# ══ 12. A verified fresh tap beats suppression ═══════════════════════════════

def test_12_tapping_the_same_product_again_gets_the_card_again() -> None:
    """A customer who taps a product again is asking for it again. Answering
    that with a suppressed card would be the platform overruling an explicit
    selection, so the tap sits above suppression in the precedence."""
    both = pp.PresentationContext(tapped_product_id=14, last_card_product_id=14)
    final, session = run_seam(draft("تفضل"), [], presentation=both)
    assert (card_of(final) or {})["product_id"] == 14
    assert shape_of(session)["shape_reason"] == pp.TAP_SELECTED


# ══ 13. A valid model-requested shape comes first ════════════════════════════

def test_13_a_model_requested_list_outranks_a_tap() -> None:
    """The model may legitimately decide this turn offers alternatives, even
    right after a tap. Its request already passed verification."""
    request = {rc.REQUESTED_KEY: {"product_ids": [11, 12], "button": "اختر"}}
    tap = pp.PresentationContext(tapped_product_id=12)
    final, session = run_seam(draft(cite=[11, 12], payload=request), [browsed(11, 12)],
                              presentation=tap)
    rows, _button = rc.payload_rows(final.payload)
    assert [r["id"] for r in rows] == [rc.row_id(11), rc.row_id(12)]
    assert card_of(final) is None
    assert shape_of(session)["shape_reason"] == pp.MODEL_REQUESTED


def test_13b_a_selector_still_wins_over_a_card_the_platform_determined() -> None:
    """Belt and braces: even if a determination reached ``reply_card`` beside an
    offered selector, the selector goes out and the reason says exactly why."""
    request = {rc.REQUESTED_KEY: {"product_ids": [11, 12], "button": "اختر"}}
    final, reason = rcard.finalize(draft(cite=[11, 12], payload=request), [browsed(11, 12)],
                                   selector_offered=True, determined_product_id=12)
    assert reason == rcard.SELECTOR_PREFERRED and rcard.payload_card(final.payload) is None


# ══ 14. The model's text is never altered, on any branch ═════════════════════

WRITTEN = "الحذاء الأبيض متوفر بمقاس ٤٢، وسعره ٢٩٩ ريالًا. تحب أرسل لك التفاصيل؟"


@pytest.mark.parametrize("label,observations,presentation", [
    ("card from a tap", [], pp.PresentationContext(tapped_product_id=12)),
    ("card from a focus", [read(12)], None),
    ("suppressed", [read(12)], pp.PresentationContext(last_card_product_id=12)),
    ("hydration found nothing", [], pp.PresentationContext(tapped_product_id=999)),
    ("no photo", [], pp.PresentationContext(tapped_product_id=15)),
    ("incidental only", [observed("get_order_details", {"status": "ok"}, ("order:500",))], None),
    ("nothing at all", [], None),
])
def test_14_the_models_wording_survives_every_branch(label, observations, presentation) -> None:
    final, _session = run_seam(draft(WRITTEN, cite=[12] if observations else []),
                               observations, presentation=presentation)
    assert final.text == WRITTEN, label


# ══ 15. More options than the channel shows: nothing is trimmed or faked ═════

def test_15_more_than_ten_options_keeps_every_one_of_them_in_the_answer() -> None:
    """The channel shows ten rows. Eleven options are not trimmed to fit and no
    eleventh row stands in for "more": the selector is withheld whole, with its
    reason, and every option follows the model's own sentence as a line of the
    merchant's values. A paginated list needs a durable, expiring, replay-safe
    snapshot; the proof that the delivery ledger's intent payload is not that
    store is in docs/engineering/commerce-runtime-product-presentation.md."""
    many = {i: _row(i, f"منتج تجريبي {i}", "50.00") for i in range(101, 112)}
    CATALOGUE[TENANT].update(many)
    try:
        ids = sorted(many)
        assert len(ids) == rc.MAX_CHOICES + 1
        rows = [dict(many[i]) for i in ids]
        browse = observed("search_products", {"status": "ok", "found": True, "products": rows},
                          tuple(str(r["evidence_ref"]) for r in rows))
        request = {rc.REQUESTED_KEY: {"product_ids": ids, "button": "اختر"}}
        written = "عندنا أكثر من خيار:"
        final, _session = run_seam(draft(written, cite=ids, payload=request), [browse])

        assert rc.payload_rows(final.payload)[0] == []
        assert final.payload.get(rc.WITHHELD_KEY) == rc.TOO_MANY
        assert final.text.startswith(written)
        for i in ids:
            assert many[i]["title"] in final.text, f"option {i} was dropped from the answer"
        # Nothing invented stands in for the options that did not fit.
        assert "nahla:page" not in final.text and rcard.payload_card(final.payload) is None
    finally:
        for i in many:
            CATALOGUE[TENANT].pop(i, None)


# ── The policy cannot read what it is not allowed to read ────────────────────

def test_the_policy_decides_the_same_shape_whatever_the_customer_wrote() -> None:
    """Merchant- and language-agnostic by construction: the decision is a pure
    function of structured provenance, so no phrasing changes it."""
    definitions = build_registry().definitions
    observations = [read(11)]
    shapes = {pp.decide(draft=draft(text, cite=[11]), observations=observations,
                        definitions=definitions).kind
              for text in ("أبغى أشوف القميص", "show me the shirt", "كم سعر العسل؟", "", "؟؟؟",
                           "المزيد", "more", "التالي")}
    assert shapes == {pp.SHAPE_CARD}


def test_a_tool_added_tomorrow_is_incidental_until_it_declares_otherwise() -> None:
    """Provenance is the registry's declaration, never a name. A new tool that
    happens to return products does not start driving cards by existing."""
    novel = ac.ToolDefinition(name="get_wishlist", description="fixture",
                              input_schema={"type": "object", "properties": {}},
                              result_kind="wishlist")
    obs = observed("get_wishlist", {"status": "ok", "products": [dict(CATALOGUE[TENANT][11])]},
                   (rc.product_ref(11),))
    reads = pp.provenance([obs], (*build_registry().definitions, novel))
    assert reads.candidates == () and reads.focused == ()


def test_a_failed_or_truncated_observation_states_nothing() -> None:
    failed = dataclasses.replace(read(11), ok=False)
    truncated = dataclasses.replace(browsed(11, 12), body_truncated=True)
    reads = pp.provenance([failed, truncated], build_registry().definitions)
    assert reads.candidates == () and reads.focused == ()
