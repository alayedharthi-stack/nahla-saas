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
import urllib.parse
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
from core.commerce_runtime import runtime_entry as entry  # noqa: E402

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


# ── A card the provider refused: its page follows the unchanged text ─────────
#
# Tenant 33, 2026-09-25 (sequences 48, 49): Meta refused a product card with
# #131053 "WebP image uploads are not currently supported", and the recovered
# text reached the customer without the page the model expected the button to
# open. Generic merchants below; the rule is not about one store's catalogue.

BAG_PAGE = "https://shop.example.test/ar/شنطة-يد-جلد/p41"


def _refused(text: str, button_url: str = BAG_PAGE) -> Dict[str, Any]:
    """The reserved intent of a card turn, as the ledger stores it."""
    return {"text": text, rcard.CARD_KEY: {"product_id": 41,
                                          "image_url": "https://cdn.example.test/bag.webp",
                                          "button_url": button_url, "button_label": "عرض"}}


def test_a_refused_card_carries_its_page_once_below_the_unchanged_text() -> None:
    written = "الشنطة متوفرة باللون البني وسعرها 229 ريال."
    recovered = rcard.text_only_payload(_refused(written))
    assert rcard.payload_card(recovered) is None
    assert recovered[rcard.WITHHELD_KEY] == rcard.PROVIDER_REJECTED
    assert recovered["text"].startswith(written)                       # every model word kept
    assert recovered["text"][len(written):] == "\n" + BAG_PAGE          # one line, the card's page
    assert recovered[rcard.LINK_APPENDED_KEY] == BAG_PAGE


def test_a_page_the_text_already_gives_is_not_given_twice() -> None:
    encoded = urllib.parse.quote(BAG_PAGE, safe=":/")
    for written in (f"تفضلي الرابط: {BAG_PAGE}",
                    f"تفضلي الرابط: {encoded}",                          # percent-encoded
                    f"تفضلي الرابط ({BAG_PAGE}/).",                       # slash and punctuation
                    f"تفضلي الرابط: {BAG_PAGE.replace('shop.', 'SHOP.')}"):  # host case
        recovered = rcard.text_only_payload(_refused(written))
        assert recovered["text"] == written, written
        assert rcard.LINK_APPENDED_KEY not in recovered


def test_a_page_address_a_customer_cannot_open_as_text_is_not_added() -> None:
    for page in ("https://", "https://shop", "https://shop.example.test/p/شنطة يد",
                 "https://shop.example.test/p/41‏"):
        recovered = rcard.text_only_payload(_refused("القميص متوفر.", button_url=page))
        assert recovered["text"] == "القميص متوفر.", page
        assert recovered[rcard.WITHHELD_KEY] == rcard.PROVIDER_REJECTED


SEND_PATH_REWRITES = {
    # every rule the send path applies to a text body, one address each
    "external_research": "https://shop.example.test/p/%25D8%25B9%25D8%25B7%25D8%25B1",
    "leakage_firewall_word": "https://shop.example.test/p/debug-kit",
    "leakage_firewall_field": "https://shop.example.test/p/perfume?intent=buy",
    # independent review of #1155: a slug the handoff-promise scrub matches is
    # cut to ``https://shop.example.test/p/`` when no handoff is active
    "handoff_promise": "https://shop.example.test/p/سيتمتحويلك",
    "internal_marker": "https://shop.example.test/p/[SKU_A1]",
}


def test_a_page_the_send_path_would_rewrite_is_not_added() -> None:
    """The addition must never be why valid model text is scrubbed or replaced
    by the send path's own wording, nor arrive as a broken address. The check
    is the send path's own rules, not a list kept here."""
    from core.outbound_sanitizer import outbound_text_rewrite_rule

    written = "عطر الورد متوفر بسعر 180 ريال."
    for rule, page in SEND_PATH_REWRITES.items():
        assert outbound_text_rewrite_rule(f"{written}\n{page}") is not None, rule
        recovered = rcard.text_only_payload(_refused(written, button_url=page))
        assert recovered["text"] == written, rule
        assert rcard.LINK_APPENDED_KEY not in recovered, rule
    added = rcard.text_only_payload(_refused(written))["text"]
    assert added.endswith(BAG_PAGE) and outbound_text_rewrite_rule(added) is None


def test_a_page_address_at_the_product_view_bound_may_be_cut_and_is_not_repeated() -> None:
    """The product view cuts a page address at its bound; an address that long
    may be a cut one, which opens a page nobody published."""
    from types import SimpleNamespace

    from core.commerce_runtime import agent_live_tools as live

    slug = urllib.parse.quote("فستان-سهرة-طويل-مطرز-بالخرز-مع-أكمام-شيفون-وحزام-ساتان-لون-كحلي")
    long_page = f"https://shop.example.test/ar/{slug}/p398551325"
    assert len(long_page) > live.MAX_LINK_CHARS
    seen = live._product_view(SimpleNamespace(product_url=long_page))["product_url"]
    assert len(seen) == live.MAX_LINK_CHARS                          # cut by the view
    recovered = rcard.text_only_payload(_refused("الفستان متوفر.", button_url=seen))
    assert recovered["text"] == "الفستان متوفر."
    whole = "https://shop.example.test/p/" + "a" * (live.MAX_LINK_CHARS - 29)
    assert len(whole) == live.MAX_LINK_CHARS - 1
    assert rcard.text_only_payload(_refused("الفستان متوفر.", button_url=whole))["text"].endswith(whole)


def test_a_recovery_without_words_or_without_a_card_adds_nothing() -> None:
    assert rcard.LINK_APPENDED_KEY not in rcard.text_only_payload(_refused(""))
    plain = {"text": "أهلاً", "choices_withheld": "x"}
    assert rcard.text_only_payload(plain) == plain


def test_the_recovery_names_each_addition_to_the_models_words() -> None:
    card_payload, card_added = entry._recovery_payload(_refused("الشنطة متوفرة."))
    assert card_added == (rcard.LINK_APPENDED_REASON,)
    assert card_payload["text"].endswith(BAG_PAGE)
    unchanged, nothing = entry._recovery_payload(_refused("رابطها " + BAG_PAGE))
    assert nothing == () and unchanged["text"] == "رابطها " + BAG_PAGE


def test_a_stored_card_missing_a_field_is_not_sent() -> None:
    """A payload written by an older release, or one that lost a value, is not
    a card the transport may hand a provider."""
    assert rcard.payload_card({rcard.CARD_KEY: {"image_url": IMAGE}}) is None
    assert rcard.payload_card({}) is None
