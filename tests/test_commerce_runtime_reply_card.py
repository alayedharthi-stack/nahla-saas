"""The model offers the card; the platform composes the photo and the link.

A customer who has already chosen a product was reading a bare URL in prose.
Here the model decides that this turn answers about one product and names it by
id; the photo shown and the page the button opens come from this turn's own
tool results. A product it names without looking up is refused, and anything
the channel will not carry leaves the model's text exactly as written.

Offline and merchant-agnostic: a dress (Tenant 1's own shape), and generic
shirt, shoe and perfume rows.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import ledger_contracts as lc  # noqa: E402
from core.commerce_runtime import reply_card as rcard  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402

IMAGE = "https://cdn.example.test/nWzD/m1JuFPTZeyNjtDm9pNK32.jpg"
LINK = "https://demostore.example.test/dev-/فستان/p398551325"


def product(product_id: int, title: str, **fields: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {"product_id": product_id, "title": title, "currency": "SAR",
                           "evidence_ref": rc.product_ref(product_id),
                           "image_url": IMAGE, "product_url": LINK}
    row.update(fields)
    return row


def lookup(one: Dict[str, Any], call_id: str = "c1") -> ac.ToolObservation:
    return ac.ToolObservation(
        call_id=call_id, tool_name="get_product_details", ok=True,
        result={"status": "ok", "found": True, "product": one},
        error_code=None, error=None, evidence_refs=(rc.product_ref(one["product_id"]),),
    )


def draft(product_id: Optional[int] = None, *, cite: Optional[Sequence[int]] = None,
          label: str = "اطلب الآن", text: str = "هذا الفستان") -> ac.ReplyDraft:
    payload: Dict[str, Any] = {}
    if product_id is not None:
        request: Dict[str, Any] = {"product_id": product_id}
        if label:
            request["button_label"] = label
        payload[rcard.REQUESTED_KEY] = request
    cited = [product_id] if cite is None and product_id is not None else (cite or [])
    return ac.ReplyDraft(text=text, evidence_refs=tuple(rc.product_ref(i) for i in cited),
                         claims_commerce_facts=True, payload=payload)


DRESS = product(23, "فستان", price="289.0")


# ── The card's values come from the merchant's records ───────────────────────

def test_the_photo_and_the_link_come_from_this_turns_observation() -> None:
    final, reason = rcard.finalize(draft(23), [lookup(DRESS)], selector_offered=False)
    assert reason == rcard.OFFERED and final.kind == lc.DeliveryKind.RICH.value
    card = rcard.payload_card(final.payload)
    # The identity travels with the card: the suppression rule and the pilot log
    # both need to know which product a delivered card actually showed.
    assert card == {"image_url": IMAGE, "button_url": LINK, "button_label": "اطلب الآن",
                    "product_id": 23}


def test_the_models_text_is_carried_through_untouched() -> None:
    """The card is an affordance over the answer, never a rewrite of it."""
    written = "هذا الفستان متوفر بمقاسين، وسعره 289 ريال."
    final, _ = rcard.finalize(draft(23, text=written), [lookup(DRESS)], selector_offered=False)
    assert final.text == written


def test_a_product_the_turn_never_read_is_refused_in_verification() -> None:
    """The same rule the selector applies: a card would open a link on a
    product this turn never established, which is a commerce claim."""
    problems = ac.verify_reply_draft(draft(999, cite=[999]), [lookup(DRESS)], inbound="")
    assert any(p.code == "card_without_evidence" for p in problems)


def test_a_product_observed_but_not_cited_is_refused() -> None:
    assert rcard.unobserved_card(draft(23, cite=[]), [lookup(DRESS)]) == 23


# ── What the channel will not carry is withheld, and says why ────────────────

def test_a_product_with_no_photo_withholds_the_card_and_keeps_the_answer() -> None:
    bare = product(41, "قميص قطني أزرق", image_url="")
    final, reason = rcard.finalize(draft(41, text="القميص متوفر"), [lookup(bare)],
                                   selector_offered=False)
    assert reason == rcard.NO_IMAGE
    assert rcard.payload_card(final.payload) is None
    assert final.text == "القميص متوفر"
    assert final.payload[rcard.WITHHELD_KEY] == rcard.NO_IMAGE


def test_a_product_with_no_link_withholds_the_card() -> None:
    bare = product(42, "حذاء رياضي أبيض", product_url="")
    _final, reason = rcard.finalize(draft(42), [lookup(bare)], selector_offered=False)
    assert reason == rcard.NO_LINK


def test_a_plain_http_link_is_refused_rather_than_upgraded() -> None:
    """Guessing a scheme onto a merchant's own URL is inventing it."""
    insecure = product(43, "عطر ورد 100ml", product_url="http://demostore.example.test/p43")
    _final, reason = rcard.finalize(draft(43), [lookup(insecure)], selector_offered=False)
    assert reason == rcard.INSECURE_LINK


def test_no_button_word_means_no_card_rather_than_a_platform_phrase() -> None:
    """Meta requires text on the button, and the platform supplies none of its
    own — a fixed word here would be a customer-facing constant nobody
    approved, which is the rule the selector already follows."""
    _final, reason = rcard.finalize(draft(23, label=""), [lookup(DRESS)], selector_offered=False)
    assert reason == rcard.NO_LABEL


# ── A card and a selector are not offered together ───────────────────────────

def test_the_selector_wins_when_both_were_asked_for() -> None:
    """A customer who still has to choose is not helped by one product's photo."""
    final, reason = rcard.finalize(draft(23), [lookup(DRESS)], selector_offered=True)
    assert reason == rcard.SELECTOR_PREFERRED
    assert rcard.payload_card(final.payload) is None
    assert final.payload[rcard.WITHHELD_KEY] == rcard.SELECTOR_PREFERRED


def test_a_reply_that_asked_for_nothing_is_returned_untouched() -> None:
    plain = draft(None, text="أهلاً")
    final, reason = rcard.finalize(plain, [lookup(DRESS)], selector_offered=False)
    assert reason == rcard.NOT_REQUESTED and final is plain


# ── The request never survives into what is delivered ────────────────────────

def test_the_models_request_is_not_carried_on_the_delivered_payload() -> None:
    final, _ = rcard.finalize(draft(23), [lookup(DRESS)], selector_offered=False)
    assert rcard.REQUESTED_KEY not in final.payload


def test_the_recovery_payload_drops_the_card_and_keeps_every_word() -> None:
    final, _ = rcard.finalize(draft(23, text="هذا الفستان"), [lookup(DRESS)],
                              selector_offered=False)
    recovered = rcard.text_only_payload(final.payload)
    assert rcard.payload_card(recovered) is None
    assert recovered.get("text", final.text) == final.payload.get("text", "هذا الفستان")
    assert recovered[rcard.WITHHELD_KEY] == "provider_rejected_the_card"


def test_a_stored_card_missing_a_field_is_not_sent() -> None:
    """A payload written by an older release, or one that lost a value, is not
    a card the transport may hand a provider."""
    assert rcard.payload_card({rcard.CARD_KEY: {"image_url": IMAGE}}) is None
    assert rcard.payload_card({}) is None
