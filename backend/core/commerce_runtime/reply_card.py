"""One product shown as a card, offered by the model, composed by the platform.

The same ownership split ``reply_choices`` already draws, for the shape that
follows a choice rather than offering one. The model decides that this turn
answers about a single product and names it **by id**; everything the customer
then sees on the card — the photo and the link the button opens — is read here
from the merchant's own values **as this turn's tool results returned them**,
never from the model's words. A URL the model typed is not a fact; a URL the
catalogue returned is.

So a card can only ever show what the turn established. A product the model
names without having looked it up this turn is withheld
(``card_without_evidence``): the card would open a link the turn never read.

A card is an affordance **over** the answer, never the answer itself. When the
channel will not take it, the model's answer stays intact. If this turn read a
usable merchant product URL, a text-only delivery carries that URL as a bare
structured fact, including after a *proven* provider rejection of the card.
An unknown send is never retried. The reason rides on the delivered payload as
``card_withheld`` so it is auditable in production rather than only in a log.

Why this fits the delivery ledger unchanged: WhatsApp's interactive ``cta_url``
message carries an image header, a body and a URL button **in one message**, so
a card is one send, exactly as an interactive list is. The loop still reserves
one delivery intent per turn and the ledger still records one receipt.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import reply_choices as rc

# What the model asked for, and what the platform established. Two keys on
# purpose, for the same reason the selector keeps them apart.
REQUESTED_KEY = "requested_card"
CARD_KEY = "card"
WITHHELD_KEY = "card_withheld"
# A verified product page, frozen with the intent when no card can be shown.
# The transport adds it only to a text send, never beside a delivered card.
LINK_FALLBACK_KEY = "card_link_fallback_url"

# Meta truncates a CTA button label, so the model's word is bounded to what the
# channel will render whole. The platform has no label of its own to fall back
# on: a card without a word from the model is no card at all.
MAX_BUTTON_LABEL = 20

# Closed reasons, for the pilot log and for tests.
NOT_REQUESTED = "not_requested"
NOT_OBSERVED = "not_observed_this_turn"
NO_IMAGE = "product_has_no_image"
NO_LINK = "product_has_no_link"
INSECURE_LINK = "product_link_not_https"
# Meta requires text on a CTA button, and it is the one thing on a card the
# customer reads — so it is the model's, in the customer's own language. The
# platform supplies no default of its own, for the same reason the selector does
# not: a fixed phrase here would be a customer-facing constant nobody approved,
# and in one language whatever language the customer is writing. Without a word
# there is simply no card, whoever chose the product.
NO_LABEL = "no_button_label_offered"
OFFERED = "offered"


@dataclasses.dataclass(frozen=True)
class ProductCard:
    """A card a provider will accept, and the identity behind it."""

    product_id: int
    image_url: str
    button_url: str
    button_label: str

    def __bool__(self) -> bool:
        return bool(self.image_url and self.button_url)

    def as_payload(self) -> Dict[str, Any]:
        return {"product_id": int(self.product_id),
                "image_url": self.image_url,
                "button_url": self.button_url,
                "button_label": self.button_label}


def requested_product_id(draft: Any) -> Optional[int]:
    """The single product the model asked to show, or ``None``.

    Read from the draft's own payload under the requested key, never from the
    text. Anything that is not one usable integer is no request at all.
    """
    payload = getattr(draft, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    requested = payload.get(REQUESTED_KEY)
    if not isinstance(requested, Mapping):
        return None
    try:
        product_id = int(requested.get("product_id"))
    except (TypeError, ValueError):
        return None
    return product_id if product_id > 0 else None


def requested_label(draft: Any) -> str:
    payload = getattr(draft, "payload", None)
    requested = payload.get(REQUESTED_KEY) if isinstance(payload, Mapping) else None
    label = str((requested or {}).get("button_label") or "").strip()
    return label if 0 < len(label) <= MAX_BUTTON_LABEL else ""


def unobserved_card(draft: Any, observations: Sequence[Any]) -> Optional[int]:
    """The product a card names that this turn did not establish, if any.

    The same three conditions the selector applies, for the same reason: the
    product must have been observed in this turn, its evidence must be indexed,
    and the draft must have cited it. A card that opens a link on an unobserved
    product is a commerce claim the turn cannot support.
    """
    product_id = requested_product_id(draft)
    if product_id is None:
        return None
    from core.commerce_runtime.agent_contracts import evidence_index  # noqa: PLC0415

    known = evidence_index(observations)
    cited = set(getattr(draft, "evidence_refs", ()) or ())
    observed = rc.observed_products(observations)
    ref = rc.product_ref(product_id)
    if product_id not in observed or ref not in known or ref not in cited:
        return product_id
    return None


def _https(value: Any) -> str:
    """A link the platform will hand a provider, or nothing.

    Only ``https``. A plain-http image or button URL is refused rather than
    upgraded: guessing a scheme onto a merchant's own URL is inventing it.
    """
    text = str(value or "").strip()
    return text if text.lower().startswith("https://") else ""


def card(draft: Any, observations: Sequence[Any], *,
         determined_product_id: Optional[int] = None) -> Tuple[Optional[ProductCard], str]:
    """The card this draft may carry, and why there is none when there is not.

    Called after verification, so anything refused here is a display limit
    rather than a truth problem, and the model's text is never touched by it.

    ``determined_product_id`` is the platform's own determination — a product
    the presentation policy established from structured provenance, typically a
    verified row tap it then read itself. It is deliberately a separate
    argument from the model's request: what was asked for and what the platform
    established are never the same field, here as everywhere else. Everything
    the customer then sees is read from this turn's observations exactly as it
    is for a requested card: only the *choice of product* has a different
    author. The button word stays the model's either way, and without one there
    is no card either way.
    """
    product_id = (int(determined_product_id) if determined_product_id is not None
                  else requested_product_id(draft))
    if product_id is None:
        return None, NOT_REQUESTED
    observed = rc.observed_products(observations)
    product = observed.get(product_id)
    if product is None:
        return None, NOT_OBSERVED
    image_url = _https(product.get("image_url"))
    button_url = _https(product.get("product_url"))
    if not image_url:
        # Distinguish "the merchant has no photo" from "the photo is not
        # https": both withhold, but only one is the merchant's to fix.
        return None, NO_IMAGE if not str(product.get("image_url") or "").strip() else INSECURE_LINK
    if not button_url:
        return None, NO_LINK if not str(product.get("product_url") or "").strip() else INSECURE_LINK
    label = requested_label(draft)
    if not label:
        # The button word is the one thing on a card the customer reads, so it
        # is the model's, in the customer's own language. Without one there is
        # no card — for a card the platform determined just as much as for one
        # the model asked for.
        #
        # This is not a formality. The channel sender substitutes a fixed
        # Arabic ``display_text`` for an empty label, so a card composed without
        # a word would arrive carrying a phrase nobody wrote and no customer's
        # language chose. Refusing here is what keeps that unreachable; a
        # wire-level case pins it.
        return None, NO_LABEL
    return ProductCard(product_id=int(product_id),
                       image_url=image_url,
                       button_url=button_url,
                       button_label=label), OFFERED


def payload_card(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The card a stored payload carries, or ``None``.

    Read defensively: a payload written by an older release carries no card,
    and one whose card lost a field is not a card the transport may send.
    """
    raw = payload.get(CARD_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    image_url = _https(raw.get("image_url"))
    button_url = _https(raw.get("button_url"))
    label = str(raw.get("button_label") or "").strip()
    # A card whose word is missing is not a card the transport may send. The
    # channel sender substitutes a fixed phrase for an empty label, so handing
    # it one would put wording on the wire that nobody wrote and no customer's
    # language chose. ``card`` already refuses to compose one; this is the same
    # refusal on the read side, for a payload written by an older release.
    if not image_url or not button_url or not label:
        return None
    try:
        product_id = int(raw.get("product_id") or 0)
    except (TypeError, ValueError):
        product_id = 0
    card_view: Dict[str, Any] = {"image_url": image_url,
                                 "button_url": button_url,
                                 "button_label": label}
    # The identity behind the card, when the stored payload carries one. A
    # payload written by an older release does not, and its absence is simply
    # an unknown identity — never a wrong one.
    if product_id > 0:
        card_view["product_id"] = product_id
    return card_view


def fallback_url(payload: Mapping[str, Any]) -> str:
    """The observed HTTPS product page reserved for a text-only delivery."""
    return _https(payload.get(LINK_FALLBACK_KEY)) if isinstance(payload, Mapping) else ""


def text_with_fallback_url(payload: Mapping[str, Any]) -> str:
    """Keep every model word and add one verified URL when the card is absent."""
    written = str(payload.get("text") or "")
    url = fallback_url(payload)
    if not url or url in written or payload_card(payload) is not None:
        return written
    separator = "" if written.endswith("\n") else "\n"
    return f"{written}{separator}{url}" if written else url

# Both were asked for. A customer who still has to choose between products is
# not helped by one product's photo, so the selector is the one that goes out
# and the card is withheld with a reason that says exactly that.
SELECTOR_PREFERRED = "selector_offered_instead"


def finalize(draft: Any, observations: Sequence[Any], *,
             selector_offered: bool,
             determined_product_id: Optional[int] = None) -> Tuple[Any, str]:
    """The draft as it will be delivered, with its card or without one.

    The model's text is never touched here. A card that cannot be offered is
    simply absent, and the reason rides on the delivered payload — the answer
    itself already stood on its own prose, which is why a card is an affordance
    over it rather than a part of it.

    ``determined_product_id`` carries the presentation policy's own choice, for
    the shapes the platform decides rather than the model — a verified row tap
    above all. A determination is never silently dropped: when it cannot become
    a card, the reason is recorded on the payload just as a refused request's
    is, so a turn that failed closed says so.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415
    from core.commerce_runtime import ledger_contracts as lc  # noqa: PLC0415

    payload: Dict[str, Any] = {key: value
                               for key, value in dict(getattr(draft, "payload", None) or {}).items()
                               if key != REQUESTED_KEY}
    # "Asked for a card" means named a product. A request carrying only the
    # button wording is an offer of words for whatever card the platform shows,
    # so a turn that shows none is not a withheld request and records nothing.
    asked = requested_product_id(draft) is not None
    determined = determined_product_id is not None
    if selector_offered:
        # A customer who still has to choose between products is not helped by
        # one product's photo, whoever asked for the photo.
        if asked or determined:
            payload[WITHHELD_KEY] = SELECTOR_PREFERRED
            return dataclasses.replace(draft, payload=ac.public_copy(payload)), SELECTOR_PREFERRED
        return draft, NOT_REQUESTED

    composed, reason = card(draft, observations, determined_product_id=determined_product_id)
    if composed is not None:
        payload[CARD_KEY] = composed.as_payload()
        return dataclasses.replace(draft, kind=lc.DeliveryKind.RICH.value,
                                   payload=ac.public_copy(payload)), reason
    if reason == NOT_REQUESTED and not asked and not determined:
        return draft, reason
    payload[WITHHELD_KEY] = reason
    # The model may reasonably have left out a raw URL because it expected
    # the card's button. A missing/insecure photo or missing button word must
    # not leave the customer with a sentence pointing to a link that vanished.
    # The merchant's page URL, if any, comes from this turn's observed product.
    # No link is inferred from customer prose or from the model's prose.
    identified = (determined_product_id if determined_product_id is not None
                  else requested_product_id(draft))
    if reason in {NO_IMAGE, INSECURE_LINK, NO_LABEL} and identified is not None:
        observed = rc.observed_products(observations).get(int(identified)) or {}
        url = _https(observed.get("product_url"))
        if url:
            payload[LINK_FALLBACK_KEY] = url
    return dataclasses.replace(draft, payload=ac.public_copy(payload)), reason


def note_withheld(draft: Any, reason: str) -> Any:
    """Record on the delivered payload why a card the platform meant to show is absent.

    A determination that failed closed before a card could even be attempted —
    the tapped product could not be read, or is no longer in the catalogue —
    leaves no trace in ``card``'s own reasons, because ``card`` was never
    reached with a product. Without this the turn would go out looking like one
    that simply never wanted a card, which is a different fact about the
    merchant's catalogue. The answer itself is untouched.
    """
    from core.commerce_runtime import agent_contracts as ac  # noqa: PLC0415

    if not reason:
        return draft
    payload: Dict[str, Any] = dict(getattr(draft, "payload", None) or {})
    if CARD_KEY in payload:
        return draft
    payload[WITHHELD_KEY] = str(reason)
    return dataclasses.replace(draft, payload=ac.public_copy(payload))


def text_only_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The same reply without its card, for the bounded recovery attempt.

    Nothing of the answer is lost: the model's text stays as written. The card's
    merchant URL is kept as a structured fallback for the text transport, which
    only runs after a proven rejection of the rich send.
    """
    out = {key: value for key, value in dict(payload or {}).items() if key != CARD_KEY}
    card_view = payload_card(payload)
    if card_view is not None:
        out[WITHHELD_KEY] = "provider_rejected_the_card"
        out[LINK_FALLBACK_KEY] = card_view["button_url"]
    return out


__all__ = [
    "CARD_KEY", "INSECURE_LINK", "MAX_BUTTON_LABEL", "NOT_OBSERVED", "NOT_REQUESTED",
    "LINK_FALLBACK_KEY", "NO_IMAGE", "NO_LABEL", "NO_LINK", "OFFERED", "REQUESTED_KEY", "WITHHELD_KEY",
    "SELECTOR_PREFERRED",
    "ProductCard", "card", "finalize", "note_withheld", "payload_card", "requested_label",
    "requested_product_id", "fallback_url", "text_only_payload", "text_with_fallback_url",
    "unobserved_card",
]
