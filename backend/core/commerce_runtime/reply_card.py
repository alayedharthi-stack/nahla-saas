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
channel will not take it — no photo, no product link, a product not observed —
the *card* is withheld and the *answer* is not: the model's text goes out
unchanged, carrying whatever it already said. The reason rides on the delivered
payload as ``card_withheld`` so it is auditable in production rather than only
in a log line.

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

# Meta truncates a CTA button label; a label longer than this is the merchant's
# words cut mid-phrase, so the platform's own short label is used instead.
MAX_BUTTON_LABEL = 20

# Closed reasons, for the pilot log and for tests.
NOT_REQUESTED = "not_requested"
NOT_OBSERVED = "not_observed_this_turn"
NO_IMAGE = "product_has_no_image"
NO_LINK = "product_has_no_link"
INSECURE_LINK = "product_link_not_https"
# Meta requires text on a CTA button. The word is the model's to choose —
# the platform supplies no default of its own, for the same reason the
# selector does not: a fixed phrase here would be a second customer-facing
# constant nobody approved. Without one there is simply no card.
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
    is for a requested card; only the *choice of product* has a different
    author, and only the button word is sourced differently (below).
    """
    platform_determined = determined_product_id is not None
    product_id = int(determined_product_id) if platform_determined else requested_product_id(draft)
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
    if not label and not platform_determined:
        # A model that asked for a card chooses its own word; without one there
        # is simply no card, because the platform supplies no phrase of its own.
        return None, NO_LABEL
    # A platform-determined card was never offered a word, so none is invented
    # here either: an empty label leaves the send path's existing ``display_text``
    # in place, exactly as ``reply_choices`` leaves the list button to the
    # channel sender. No customer-facing constant is introduced.
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
    if not image_url or not button_url:
        return None
    try:
        product_id = int(raw.get("product_id") or 0)
    except (TypeError, ValueError):
        product_id = 0
    card_view: Dict[str, Any] = {"image_url": image_url,
                                 "button_url": button_url,
                                 "button_label": str(raw.get("button_label") or "").strip()}
    # The identity behind the card, when the stored payload carries one. A
    # payload written by an older release does not, and its absence is simply
    # an unknown identity — never a wrong one.
    if product_id > 0:
        card_view["product_id"] = product_id
    return card_view



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
    asked = REQUESTED_KEY in (getattr(draft, "payload", None) or {})
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

    Nothing of the answer is lost: the card carried no words of its own, so
    removing it leaves the model's text exactly as it was written.
    """
    out = {key: value for key, value in dict(payload or {}).items() if key != CARD_KEY}
    if payload_card(payload) is not None:
        out[WITHHELD_KEY] = "provider_rejected_the_card"
    return out


__all__ = [
    "CARD_KEY", "INSECURE_LINK", "MAX_BUTTON_LABEL", "NOT_OBSERVED", "NOT_REQUESTED",
    "NO_IMAGE", "NO_LABEL", "NO_LINK", "OFFERED", "REQUESTED_KEY", "WITHHELD_KEY",
    "SELECTOR_PREFERRED",
    "ProductCard", "card", "finalize", "note_withheld", "payload_card", "requested_label",
    "requested_product_id", "text_only_payload",
    "unobserved_card",
]
